"""kitlib.agentview: everything one role is given and held to, on one host (decision 18 in
docs/multi-host-plan.md section 7; docs/content.md "Agent view"). tools/agent_view.py prints it.

  view(kit, repo, name, host, renderer, ...)   [Section(title, lines)], in the order the agent
                                               receives them, then the enforced side
  UnknownRole                                  `name` is no role file's (.known: the roles there)

Every section is built from the code that renders or enforces it, never from a list kept here:

  Role file                the files the host's ContentRenderer writes from this role (the ones
                           whose bytes change when only this role's body does), and their text
  Repository instructions  the host's `instructions_file` token in `repo`, and the files its
                           `@<path>` lines import
  User instructions        that file name in the host's per-user folder (hosts.json profile
                           state_directory, or the folder its `config_dir_env` names); never read
  Kit header               kitlib.host.header.text(kit), what every host's start hook adds
  At start                 the host's own start text past the header (`host_start`, a host
                           adapter's start.text when it has one), else the PM checklist
                           (kitlib.pmstart) or the coordinator rules (kitlib.coordinatorstart)
  Skills                   content/skills, when the host has skills
  Repo profile             the host's `profile_path` token in `repo` (and `coordinator_profile_path`
                           when the role's body names it)
  Model, mode and writes   the role file, the host's tier map and capability map
  Backlog rights           kitlib.backlog.roles: role_of and allows over every ACTIONS row
  .kit.json switches       the guard's own Ctx and kitlib.guard.rolepolicy.mode: the main
                           checkout's config, guard.role_policy, the roles entry, data_roots
  Guard rules for its mode kitlib.guard.rolepolicy's mode tables and its own docstring lines

The host is chosen at the edge: `renderer` is the host's ContentRenderer, `values` its one-value
tokens and `tools` its capability map (its adapter's content module; None when it has none).
No printing, no exit; a check that fails is a line saying so, never raised, except UnknownRole and
the ContentError of a content/ that does not load.
"""
import collections
import dataclasses
import os
import re

from kitlib import content as kcontent
from kitlib.host import header
from kitlib.host.content import Content, tokens_in

Section = collections.namedtuple('Section', 'title lines')
# A line `@<path>` alone in an instructions file imports that file (relative to the file's folder).
IMPORT = re.compile(r'^@(\S+)\s*$', re.M)
IMPORT_DEPTH = 5
MARK = '\n<!-- agent_view: this role -->\n'
DESCRIPTION = re.compile(r'^description:\s*(.*)$', re.M)
SESSION, DISPATCHED = 'SessionStart', 'SubagentStart'


class UnknownRole(ValueError):
    """No role file defines the name. `known` holds every role there is."""

    def __init__(self, name, known):
        ValueError.__init__(self, 'unknown role %r: the roles are %s' % (name, ', '.join(known)))
        self.name, self.known = name, tuple(known)


def resolve(name, content):
    """The Role `name` (or the role an old name in kitlib.content.ALIASES became) in `content`."""
    want = kcontent.ALIASES.get(name, name)
    try:
        return content.role(want)
    except KeyError:
        raise UnknownRole(name, content.role_names()) from None


def view(kit, repo, name, host, renderer, values=None, tools=None, host_start=None, environ=None,
         brief=False, checklists=None, ctx=None):
    """The sections for role `name` on `host` (a kitlib.host.registry.Host) in the checkout `repo`,
    for the kit at `kit`. `brief`: paths only, no file text and no start checks run. `checklists`
    ({'pm': fn(), 'coordinator': fn(repo)}) and `ctx` (a kitlib.guard.context.Ctx) stand in for
    the live ones (tests)."""
    environ = os.environ if environ is None else environ
    values = values or {}
    content = kcontent.load(kit)
    role = resolve(name, content)
    event = SESSION if role.run == 'session' else DISPATCHED
    return [
        role_file(kit, role, content, renderer, brief),
        instructions(repo, values, brief),
        user_instructions(host, values, environ),
        Section('Kit header (%s)' % event, ['kitlib.host.header.text, added by every host\'s start hook:',
                                            header.text(kit)]),
        start(kit, repo, role, event, host_start, brief, checklists),
        skills(content, role, host),
        profile(repo, role, values),
        model_mode(role, host, renderer, tools),
        backlog(role),
        switches(kit, repo, role, ctx),
        guard_rules(role),
    ]


def _rel(path, root):
    try:
        return os.path.relpath(path, root).replace(os.sep, '/')
    except ValueError:                          # another drive: the path as it is
        return path


def _read_text(path):
    try:
        with open(path, 'rb') as f:
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return None


def role_files(role, content, renderer):
    """{path: bytes} of the files `renderer` writes from `role` alone: the ones whose bytes change
    when only its body does (skills left out)."""
    base = Content(roles=content.roles, skills=())
    marked = Content(roles=tuple(dataclasses.replace(r, body=r.body + MARK) if r is role else r
                                 for r in content.roles), skills=())
    want, other = renderer.render(base), renderer.render(marked)
    return {p: d for p, d in sorted(want.items()) if other.get(p) != d}


def role_file(kit, role, content, renderer, brief):
    lines = ['source: %s/%s.md (edit it, then python tools/render_content.py --write)'
             % (kcontent.ROLES_REL, role.name)]
    files = role_files(role, content, renderer)
    if not files:
        lines.append('the host writes no file for this role')
    for path, data in files.items():
        on_disk = _read_text(os.path.join(kit, *path.split('/')))
        state = '' if on_disk is None or on_disk.encode('utf-8') == data else \
            ' (the committed copy differs: render again)'
        lines.append('rendered: %s%s' % (path, state))
        if not brief:
            lines.append(data.decode('utf-8', 'replace'))
    if role.run == 'session':
        lines.append('run: session (started only as its own launched session, never dispatched)')
    return Section('Role file', lines)


def imports(path, seen=None, depth=0):
    """[(absolute path, depth)] of `path` and every file it imports with an `@<path>` line, each
    once, IMPORT_DEPTH hops at most; a missing file is listed and not followed."""
    seen = set() if seen is None else seen
    key = os.path.normcase(os.path.abspath(path))
    if key in seen or depth > IMPORT_DEPTH:
        return []
    seen.add(key)
    out = [(os.path.abspath(path), depth)]
    text = _read_text(path)
    for target in IMPORT.findall(text or ''):
        target = os.path.expanduser(target)
        full = target if os.path.isabs(target) else os.path.join(os.path.dirname(path), target)
        out.extend(imports(full, seen, depth + 1))
    return out


def instructions(repo, values, brief):
    name = values.get('instructions_file')
    if not name:
        return Section('Repository instructions', ['the host names no instructions file (no instructions_file token)'])
    lines = []
    for path, depth in imports(os.path.join(repo, name)):
        text = _read_text(path)
        where = _rel(path, repo)
        label = '%s%s' % ('  ' * depth + ('imports ' if depth else ''), where)
        lines.append(label if text is not None else label + ' (not there)')
        if text is not None and not brief:
            lines.append(text.rstrip('\n'))
    return Section('Repository instructions', lines)


def user_instructions(host, values, environ):
    name = values.get('instructions_file')
    if not name:
        return Section('User instructions', ['the host names no instructions file'])
    env = values.get('config_dir_env')
    folder = environ.get(env) if env else None
    where = os.path.join(folder, name) if folder else '~/%s/%s' % (host.profile.state_directory, name)
    moved = ' (%s moves the folder)' % env if folder else ''
    return Section('User instructions', ['%s%s: not read by the kit, may exist' % (where, moved)])


def start(kit, repo, role, event, host_start, brief, checklists):
    title = 'At start (%s), besides the header' % event
    if host_start is not None:
        if brief:
            return Section(title, ['the host\'s start text (its adapter\'s start.text); run without --brief to see it'])
        try:
            text = host_start(event, kit, {'cwd': os.path.abspath(repo)},
                              {'PM_KIT_ROLE': role.name})
        except Exception as e:
            return Section(title, ['could not make it: %s: %s' % (type(e).__name__, e)])
        text = text[len(header.text(kit)):].lstrip('\n') if text.startswith(header.text(kit)) else text
        return Section(title, [text] if text else ['nothing'])
    if role.run != 'session':
        return Section(title, ['nothing: a dispatched role gets the header alone'])
    from kitlib import coordinatorstart
    from kitlib.backlog import roles as broles
    checklists = checklists or {}
    if role.name == broles.PM:
        how, fn = 'kitlib.pmstart.checklist (the PM settings\' SessionStart hook)', checklists.get('pm', _pm_checklist)
        args = ()
    elif coordinatorstart.is_coordinator({'PM_KIT_ROLE': role.name}):
        how, fn = 'kitlib.coordinatorstart.render (the coordinator SessionStart hook)', \
            checklists.get('coordinator', _coordinator_checklist)
        args = (repo,)
    else:
        return Section(title, ['nothing: no start checklist for this role'])
    if brief:
        return Section(title, ['made by %s; run without --brief to see it' % how])
    try:
        text = fn(*args)
    except Exception as e:
        text = 'could not check: %s: %s' % (type(e).__name__, e)
    return Section(title, ['made by %s:' % how, text or 'nothing'])


def _pm_checklist():
    from kitlib import pmstart
    cfg = pmstart.load_config()
    if cfg is None:
        return 'no pm_start config in the machine config: the hook skips the checklist'
    return pmstart.checklist(cfg)


def _coordinator_checklist(repo):
    from kitlib import coordinatorstart
    return coordinatorstart.render(env={'PM_KIT_ROLE': 'coordinator'}, cwd=os.path.abspath(repo))


def skills(content, role, host):
    if not host.capabilities.skills:
        return Section('Skills', ['none: the host has no skills (hosts.json capabilities.skills)'])
    lines = []
    if not role.all_tools and 'skill' not in role.capabilities:
        lines.append('the role\'s capabilities do not name `skill`: where the host gives a tool list, it cannot load these')
    for s in content.skills:
        m = DESCRIPTION.search(dict(s.files).get('SKILL.md', b'').decode('utf-8', 'replace'))
        lines.append('%s (%s/%s/SKILL.md)%s' % (s.name, kcontent.SKILLS_REL, s.name,
                                                ': ' + m.group(1).strip() if m else ''))
    return Section('Skills', lines)


def profile(repo, role, values):
    keys = ['profile_path'] + [k for k in ('coordinator_profile_path',)
                               if (k, None) in tokens_in(role.body)]
    lines = []
    for key in keys:
        rel = values.get(key)
        if not rel:
            lines.append('%s: the host names none' % key)
            continue
        there = os.path.isfile(os.path.join(repo, *rel.split('/')))
        lines.append('%s: %s' % (key, rel if there else 'none (%s is not in the repository)' % rel))
    return Section('Repo profile', lines)


def model_mode(role, host, renderer, tools):
    tiers = renderer.tier_models()
    lines = ['tier %s -> model %s%s' % (role.tier, role.model_for(host.name, tiers),
                                         '; effort %s' % role.effort if role.effort else ''),
             'sandbox: %s' % role.sandbox,
             'mode: %s' % (role.mode or 'none (the role policy never holds it)'),
             'writes: %s' % (', '.join(role.writes) if role.writes else 'none beyond its mode'),
             'group: %s; run: %s' % (role.group or 'none', role.run or 'dispatched')]
    if role.all_tools:
        lines.append('capabilities: all (no tool list: every tool the host has)')
    elif tools is None:
        lines.append('capabilities: %s (the host gives no tool map)' % ', '.join(role.capabilities))
    else:
        lines.append('capabilities -> host tools:')
        lines.extend('  %s -> %s' % (c, tools.get(c, '(none)')) for c in role.capabilities)
    return Section('Model, mode and capabilities', lines)


def backlog(role):
    from kitlib.backlog import roles as broles
    subagent = role.run != 'session'
    who = broles.role_of(role.name, subagent)
    lines = ['as %s: %s' % ('a dispatched subagent' if subagent else 'a launched session',
                            'role %s' % who if who else 'unlisted (only risk %s)' % ', '.join(broles.UNLISTED_MAY))]
    for action, row in broles.ACTIONS.items():
        verbs = ', '.join(row.verbs) or 'a raw write'
        if broles.allows(action, who):
            say = 'yes'
        else:
            facts = [f for r, f in row.when if r == who]
            say = 'only with %s' % ', '.join(facts) if facts else 'no'
        lines.append('%s (%s; risk %s): %s' % (action, verbs, row.risk, say))
    return Section('Backlog rights (kitlib/backlog/roles.py)', lines)


def _action(repo, role):
    from kitlib.host import Action
    cwd = os.path.abspath(repo)
    probe = os.path.join(cwd, 'agent_view.probe')
    if role.run == 'session':
        return Action(kind='edit', path=probe, cwd=cwd, role=role.name)
    return Action(kind='edit', path=probe, cwd=cwd, agent='agent_view', agent_role=role.name)


def _ctx(kit, ctx):
    if ctx is not None:
        return ctx
    from kitlib.guard.context import Ctx
    return Ctx(kit=kit)


def switches(kit, repo, role, ctx=None):
    from kitlib import config as kconfig
    from kitlib.guard import rolepolicy
    title = '.kit.json switches'
    try:
        ctx = _ctx(kit, ctx)
        top = ctx.main_top(os.path.abspath(repo))
        if not top:
            return Section(title, ['%s is in no git checkout: no .kit.json is read' % repo])
        cfg = ctx.config_at(top)
        if cfg is None:
            return Section(title, ['%s/.kit.json does not load: %s (the guard refuses every call)'
                                   % (top, ctx.config_error_at(top))])
        action = _action(repo, role)
        lines = ['%s (%s)' % (os.path.join(top, '.kit.json'),
                              'found' if getattr(cfg, 'found', True) else 'none: every default')]
        lines.append('guard.role_policy: %s' % ('true: roles are held to their mode' if rolepolicy.enabled(cfg)
                                                 else 'false: no role is held, whatever its mode'))
        entries = cfg.section('roles')
        for n in kcontent.names_of(role.name):
            if n in entries:
                lines.append('roles.%s: %s' % (n, entries[n]))
        held, m = rolepolicy.mode(action, ctx)
        lines.append('mode as the guard reads it: %s' % (m or 'none (not held)'))
        perms = kconfig.permissions(action, kconfig.roles(cfg))
        lines.append('edit_main_checkout: %s' % perms.get('edit_main_checkout'))
        roots = cfg.section('guard').get('data_roots') or []
        lines.append('guard.data_roots: %s' % (', '.join('%s (%s)' % (r['path'], r['mode']) for r in roots)
                                               or 'none'))
        return Section(title, lines)
    except Exception as e:
        return Section(title, ['could not read: %s: %s' % (type(e).__name__, e)])


def rule_text():
    """{rule name: its text} from kitlib.guard.rolepolicy's docstring (the `role-...` entries)."""
    from kitlib.guard import rolepolicy
    out, name = {}, None
    for line in (rolepolicy.__doc__ or '').splitlines():
        m = re.match(r'^ {4}(role-[a-z]+)\s+(.*)$', line)
        if m:
            name = m.group(1)
            out[name] = out.get(name, '') + (' ' if name in out else '') + m.group(2).strip()
        elif name and re.match(r'^ {18}\S', line):
            out[name] += ' ' + line.strip()
        else:
            name = None
    return out


def guard_rules(role):
    from kitlib.guard import rolepolicy
    from kitlib.host import registry
    title = 'Guard rules for its mode (kitlib/guard/rolepolicy.py)'
    m = role.mode
    if m is None:
        return Section(title, ['none: the role names no mode'])
    texts = rule_text()
    lines = []
    if m in rolepolicy.NO_EDIT:
        lines.append('role-edit: %s%s' % (texts.get('role-edit', ''),
                                          '; its writes: %s' % ', '.join(role.writes) if role.writes else ''))
    lines.append('role-workdir: %s' % texts.get('role-workdir', ''))
    lines.append('role-shell: %s' % texts.get('role-shell', ''))
    lines.append('role-lock: %s; this mode\'s verbs: %s' % (texts.get('role-lock', ''),
                                                           rolepolicy.verbs_text(rolepolicy.LOCK_VERBS[m])))
    if m in rolepolicy.READ_ONLY_CHECKOUT:
        lines.append('write-jail: its checkout is read-only to the shell')
    if m in rolepolicy.OWNER_PATHS_READ_ONLY:
        lines.append('write-jail: the owner paths are read-only: %s' % ', '.join(registry.union('owner_paths')))
    lines.append('(these hold only where .kit.json sets guard.role_policy; the shared guard rules hold for every role)')
    return Section(title, lines)
