"""kitlib.agentview: everything one role is given and held to, on one host (decision 18 in
docs/multi-host-plan.md section 7; docs/content.md "Agent view"). tools/agent_view.py prints it.

  view(kit, repo, name, host, renderer, ...)   [Section(title, lines)]: GIVEN sections in the order
                                               the agent receives them, then the HELD ones
  UnknownRole                                  `name` is no role file's (.known: the roles there)

Every section is built from the code that renders or enforces it, never from a list kept here.
The host's side comes only through the host contract (kitlib.host; docs/new-host.md), which the
conformance suite pins to what the host writes:

  Role file                renderer.role_path(role), and that file as renderer.render writes it
  Repository instructions  renderer.tokens()'s `instructions_file` in `repo`, and the files its
                           `@<path>` lines import
  User instructions        that file name in the host's per-user folder (the `user_folder` token,
                           or the folder its `config_dir_env` names); never read
  Kit header               kitlib.host.header.text(kit), what every host's start hook adds
  At start                 the adapter's start_text(event, kit, payload, environ) past the header,
                           for a session marked with the role (optional: unknown without it)
  Skills                   content/skills, when the host has skills (hosts.json)
  Repo profile             the `profile_path` token in `repo` (and `coordinator_profile_path` when
                           the role's body names it); no profile_path file: the kit's
                           content/default-profile.md, the roles' fallback (decision 26)
  Model, tools and mode    the role file; renderer.settings(role) (what the host's file sets) and
                           renderer.tools() (its capability -> tool map)
  Guard rules              kitlib.guard.rolepolicy.rules_for(mode) and its LOCK_VERBS
  .kit.json switches       the guard's own Ctx and rolepolicy.mode: the main checkout's config,
                           guard.role_policy, the roles entry, data_roots
  Backlog rights           kitlib.backlog.roles: role_of and allows over every ACTIONS row

No printing, no exit; a check that fails is a line saying so, never raised, except UnknownRole and
the ContentError of a content/ that does not load.
"""
import collections
import os
import re

from kitlib import content as kcontent
from kitlib.host import header
from kitlib.host.content import tokens_in

Section = collections.namedtuple('Section', 'title lines')
GIVEN = ('Role file', 'Repository instructions', 'User instructions', 'Kit header', 'At start', 'Skills',
         'Repo profile')
HELD = ('Model, tools and mode', 'Guard rules', '.kit.json switches', 'Backlog rights')
# A line `@<path>` alone in an instructions file imports that file (relative to the file's folder).
IMPORT = re.compile(r'^@(\S+)\s*$', re.M)
IMPORT_DEPTH = 5
INDENT = '    '
DESCRIPTION = re.compile(r'^description:\s*(.*)$', re.M)
SESSION, DISPATCHED = 'SessionStart', 'SubagentStart'
DEFAULT_PROFILE = 'content/default-profile.md'   # a role's profile when the repo has none


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


def view(kit, repo, name, host, renderer, start_text=None, environ=None, brief=False, ctx=None):
    """The sections for role `name` on `host` (a kitlib.host.registry.Host, whose ContentRenderer is
    `renderer` and whose adapter's start_text is `start_text`, None when it has none) in the
    checkout `repo`, for the kit at `kit`. `brief`: paths only, no file text and no start checks
    run. `ctx` (a kitlib.guard.context.Ctx) stands in for the live one (tests)."""
    environ = os.environ if environ is None else environ
    content = kcontent.load(kit)
    role = resolve(name, content)
    values = renderer.tokens()
    event = SESSION if role.run == 'session' else DISPATCHED
    return [
        role_file(kit, role, content, renderer, brief),
        instructions(repo, values, brief),
        user_instructions(values, environ),
        kit_header(kit, event, brief),
        start(kit, repo, role, event, start_text, environ, brief),
        skills(content, role, host, renderer, brief),
        profile(kit, repo, role, values),
        model_mode(role, renderer),
        guard_rules(role),
        switches(kit, repo, role, ctx),
        backlog(role),
    ]


def block(text):
    """`text` indented, so a file's own headings do not read as this view's."""
    return '\n'.join(INDENT + line if line else '' for line in text.rstrip('\n').split('\n'))


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


def role_file(kit, role, content, renderer, brief):
    lines = ['source: %s/%s.md (edit it, then: python tools/render_content.py --write)'
             % (kcontent.ROLES_REL, role.name)]
    path = renderer.role_path(role.name)
    data = renderer.render(content).get(path)
    if data is None:
        return Section('Role file', lines + ['the host\'s renderer writes no %s' % path])
    on_disk = _read_text(os.path.join(kit, *path.split('/')))
    state = '' if on_disk is not None and on_disk.encode('utf-8') == data else \
        ' (the committed copy differs: render again)'
    lines.append('the host loads: %s%s' % (path, state))
    if role.run == 'session':
        lines.append('run: session (only ever its own launched session, never dispatched)')
    if not brief:
        lines.append(block(data.decode('utf-8', 'replace')))
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
        label = '  ' * depth + ('imports ' if depth else '') + _rel(path, repo)
        lines.append(label if text is not None else label + ' (not there)')
        if text is not None and not brief:
            lines.append(block(text))
    return Section('Repository instructions', lines)


def user_instructions(values, environ):
    name, home = values.get('instructions_file'), values.get('user_folder')
    if not name or not home:
        return Section('User instructions', ['the host names no per-user instructions file'])
    env = values.get('config_dir_env')
    folder = environ.get(env) if env else None
    where = os.path.join(folder, name) if folder else '%s/%s' % (home, name)
    moved = ' (%s moves the folder)' % env if folder else ''
    return Section('User instructions',
                   ['%s%s: the user\'s own, if it exists; the kit never reads it' % (where, moved)])


def kit_header(kit, event, brief):
    title = 'Kit header (%s)' % event
    lines = ['kitlib.host.header.text: the kit folder, and what <kit> and the other placeholders mean']
    if not brief:
        lines.append(block(header.text(kit)))
    return Section(title, lines)


def _mark(environ, role):
    from kitlib import sessionmark
    env = dict(environ)
    env[sessionmark.ENV] = role.name
    return env


def start(kit, repo, role, event, start_text, environ, brief):
    title = 'At start (%s), past the header' % event
    if start_text is None:
        return Section(title, ['not known: the host\'s adapter has no start_text (docs/new-host.md)'])
    if brief:
        return Section(title, ['the adapter\'s start_text, for a session marked %s; run without --brief to see '
                               'it (it runs the start checks)' % role.name])
    try:
        text = start_text(event, kit, {'cwd': os.path.abspath(repo)}, _mark(environ, role))
    except Exception as e:
        return Section(title, ['could not make it: %s: %s' % (type(e).__name__, e)])
    head = header.text(kit)
    text = (text[len(head):] if text.startswith(head) else text).strip('\n')
    return Section(title, [block(text)] if text else ['nothing'])


def skills(content, role, host, renderer, brief):
    if not host.capabilities.skills:
        return Section('Skills', ['none: the host has no skills (hosts.json capabilities.skills)'])
    lines = []
    listed = dict(renderer.settings(role)).get('tools')
    tool = (renderer.tools() or {}).get('skill')
    if listed is not None and 'skill' not in role.capabilities:
        lines.append('its tool list has no %s tool: it cannot load these' % (tool or 'skill'))
    if brief:
        lines.append('%d, in %s/: %s' % (len(content.skills), kcontent.SKILLS_REL,
                                          ', '.join(s.name for s in content.skills)))
        return Section('Skills', lines)
    for s in content.skills:
        m = DESCRIPTION.search(dict(s.files).get('SKILL.md', b'').decode('utf-8', 'replace'))
        lines.append('%s: %s' % (s.name, m.group(1).strip() if m else '(no description)'))
    return Section('Skills', lines)


def profile(kit, repo, role, values):
    """The profile in use: the repository's, or, with none (it is opt-in: decision 26), the kit's
    default profile that the role falls back to for `profile_path`."""
    keys = ['profile_path'] + [k for k in ('coordinator_profile_path',)
                               if (k, None) in tokens_in(role.body)]
    lines = []
    for key in keys:
        rel = values.get(key)
        there = bool(rel) and os.path.isfile(os.path.join(repo, *rel.split('/')))
        if there:
            lines.append('%s: %s' % (key, rel))
            continue
        why = 'the host names none' if not rel else 'none (%s is not in the repository)' % rel
        if key == 'profile_path':
            why += '; in use: the kit\'s default, %s' % os.path.join(kit, *DEFAULT_PROFILE.split('/'))
        lines.append('%s: %s' % (key, why))
    return Section('Repo profile', lines)


def model_mode(role, renderer):
    lines = ['role file: tier %s, effort %s, sandbox %s' % (role.tier, role.effort or 'unset', role.sandbox)]
    settings = renderer.settings(role)
    lines.append('the host\'s file sets: %s' % ('; '.join('%s %s' % kv for kv in settings) or 'nothing'))
    if 'model' not in dict(settings):
        lines.append('  (no model in it: the host\'s own default model runs)')
    tools = renderer.tools()
    if role.all_tools:
        lines.append('capabilities: all (every tool the host has)')
    elif tools is None:
        lines.append('capabilities: %s (the host maps them to no tool names)' % ', '.join(role.capabilities))
    else:
        lines.append('capabilities, as the host\'s tools: %s'
                     % ', '.join('%s -> %s' % (c, tools.get(c, '(none)')) for c in role.capabilities))
    lines.append('mode: %s' % (role.mode or 'none (the role policy never holds it)'))
    if role.writes:
        lines.append('writes (allowed despite its mode): %s' % ', '.join(role.writes))
    lines.append('backlog group: %s; run: %s' % (role.group or 'none', role.run or 'dispatched'))
    return Section('Model, tools and mode', lines)


def guard_rules(role):
    from kitlib.guard import rolepolicy
    from kitlib.host import registry
    m = role.mode
    title = 'Guard rules for mode %s (kitlib/guard/rolepolicy.py RULES)' % (m or 'none')
    if m is None:
        return Section(title, ['none: the role names no mode, so only the shared guard rules hold it'])
    owner = [t for r, modes, t in rolepolicy.RULES if r == 'write-jail' and modes is rolepolicy.OWNER_PATHS_READ_ONLY]
    lines = []
    for rule, text in rolepolicy.rules_for(m):
        if rule == 'role-lock':
            text += '; its verbs: %s' % rolepolicy.verbs_text(rolepolicy.LOCK_VERBS[m])
        elif rule == 'role-edit' and role.writes:
            text += ' (its writes: %s)' % ', '.join(role.writes)
        elif text in owner:
            text += ' (%s)' % ', '.join(registry.union('owner_paths'))
        lines.append('- %s: %s' % (rule, text))
    lines.append('These hold only where the repository\'s .kit.json turns guard.role_policy on (next '
                 'section); the shared guard rules hold every role.')
    return Section(title, lines)


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


def backlog(role):
    """Its rights over every kitlib.backlog.roles ACTIONS row, grouped by the answer."""
    from kitlib.backlog import roles as broles
    subagent = role.run != 'session'
    who = broles.role_of(role.name, subagent)
    lines = ['as %s: %s' % ('a dispatched subagent' if subagent else 'a launched session',
                            'role %s' % who if who else 'unlisted (only risk %s)' % ', '.join(broles.UNLISTED_MAY))]
    # The grant facts (broles.FACTS) in plain words; one missing here shows as `with <fact>`.
    plain = {broles.OWN_PULL: 'on a record it pulled itself', broles.VIA_LAND: 'through the landing tool'}
    answers = {}
    for action, row in broles.ACTIONS.items():
        if broles.allows(action, who):
            say = 'may'
        else:
            facts = [f for r, f in row.when if r == who]
            say = 'only %s' % ' or '.join(plain.get(f, 'with ' + f) for f in facts) if facts else 'may not'
        answers.setdefault(say, []).append('%s (%s)' % (action, ', '.join(row.verbs) or 'a raw write'))
    order = lambda say: (say != 'may', say == 'may not', say)     # noqa: E731
    lines.extend('%s: %s' % (say, ', '.join(answers[say])) for say in sorted(answers, key=order))
    lines.append('each action\'s risk and who may take it: kitlib/backlog/roles.py ACTIONS')
    return Section('Backlog rights (kitlib/backlog/roles.py)', lines)
