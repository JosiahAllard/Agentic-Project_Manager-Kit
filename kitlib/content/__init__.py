"""kitlib.content: render `content/` into each host's files (docs/content.md).

`load(root)` reads every role (`content/roles/*.md`, kitlib.host.content.parse_role) and every
skill folder (`content/skills/<name>/`, its files as bytes). `render(root, renderers)` asks each
host's ContentRenderer for its files. `drift` compares them with the committed files: a file that
differs, one that is missing, and one in a folder a renderer owns that it no longer writes (stale).
`write` makes the committed files what the renderers say, and removes the stale ones.

`load_kit(root)` reads `kit.json`, the kit's one name and version (ARCHITECTURE.md "Packaging"). A
renderer that also has `manifests(kit, content)` (a host's plugin manifests, which carry that
version) is asked for those files too, and they are checked and written like the rest.

The renderers are passed in: this package names no host and imports none (the host is chosen at
the edge, tools/render_content.py). Given `tiers` ({host: {tier: model}}, every host's, whichever
renderers run), `render` also remakes docs/ROSTER.md's tables (kitlib.roster), so the roster is
checked and written like the host files. Pure file work: no printing, no exit.

`roster(kit)` is the kit's role list, read from the role files and nothing else (decision 17 in
docs/multi-host-plan.md section 7): a RosterEntry (name, mode, tier, group, run) per role, by name.
The launcher's KIT_AGENTS and the backlog policy's groups (kitlib.backlog.roles) are read from it.

`names_of(name)` is a role's name with its old or new one (ALIASES). `role_modes(kit)` is {role: mode} from the role files under the kit root `kit`, for the guard's
role policy (docs/guard.md "Role policy"); `role_mode(name, kit)` one role's, an old name
(ALIASES) read as its new one; `role_writes(name, kit)` the paths its `writes` names. These and
roster share one read of the role files per kit root, cached, so the guard's hook path parses
them once. None raises: no `content/roles` folder gives {}, and a role file that cannot be read
or parsed is left out, so the policy is off for that role rather than the guard failing.
"""
import collections
import json
import os
import re

from kitlib.host.content import MODES, Content, ContentError, Skill, parse_role

ROLES_REL = 'content/roles'
SKILLS_REL = 'content/skills'
KIT_REL = 'kit.json'
ROSTER_REL = 'docs/ROSTER.md'
SKIP_DIRS = ('__pycache__',)
# a role's old name -> its name now, for one release (docs/ROSTER.md; the launcher's AGENT_ALIASES)
ALIASES = {'opus-worker': 'senior-worker'}
_ROLES = {}
_KIT_NAME = re.compile(r'[a-z][a-z0-9-]*\Z')
_VERSION = re.compile(r'\d+\.\d+\.\d+\Z')
# One role in the roster: its name, mode (MODES or None), tier, group (kitlib.host.content.GROUPS
# or None: a role with no mode) and run ('session' for a launched-only role, else None).
RosterEntry = collections.namedtuple('RosterEntry', 'name mode tier group run')


def names_of(name):
    """(name, then every other name the same role goes by in ALIASES): its new name for an old
    one, its old names for a new one."""
    out = [name]
    for old, new in ALIASES.items():
        other = new if old == name else old if new == name else None
        if other and other not in out:
            out.append(other)
    return tuple(out)


def load_kit(root):
    """{"name", "version"} from `<root>/kit.json`: exactly those keys, a lowercase name and an
    N.N.N version; ContentError otherwise."""
    try:
        with open(_join(root, KIT_REL), 'rb') as f:
            kit = json.loads(f.read().decode('utf-8'))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ContentError('%s cannot be read: %s' % (KIT_REL, e)) from None
    if not isinstance(kit, dict) or set(kit) != {'name', 'version'} \
            or not isinstance(kit['name'], str) or not _KIT_NAME.match(kit['name']) \
            or not isinstance(kit['version'], str) or not _VERSION.match(kit['version']):
        raise ContentError('%s must be {"name": "<lowercase name>", "version": "N.N.N"}' % KIT_REL)
    return kit


def _join(root, rel):
    return os.path.join(root, *rel.split('/'))


def _files_under(folder):
    """Every file below `folder`, as posix paths relative to it, sorted."""
    out = []
    for dirpath, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        rel = os.path.relpath(dirpath, folder)
        for f in files:
            out.append(f if rel == '.' else '/'.join(rel.split(os.sep) + [f]))
    return sorted(out)


def load_roles(root):
    folder = _join(root, ROLES_REL)
    roles = []
    for n in sorted(os.listdir(folder)):
        if not n.endswith('.md'):
            continue
        with open(os.path.join(folder, n), 'rb') as f:
            role = parse_role(f.read(), '%s/%s' % (ROLES_REL, n))
        if role.name != n[:-3]:
            raise ContentError('%s/%s: name %s is not the file name' % (ROLES_REL, n, role.name))
        roles.append(role)
    if not roles:
        raise ContentError('%s: no roles' % ROLES_REL)
    return tuple(roles)


def load_skills(root):
    folder = _join(root, SKILLS_REL)
    skills = []
    for n in sorted(os.listdir(folder)):
        d = os.path.join(folder, n)
        if not os.path.isdir(d) or n in SKIP_DIRS:
            continue
        files = []
        for rel in _files_under(d):
            with open(os.path.join(d, *rel.split('/')), 'rb') as f:
                files.append((rel, f.read()))
        if 'SKILL.md' not in dict(files):
            raise ContentError('%s/%s: no SKILL.md' % (SKILLS_REL, n))
        skills.append(Skill(name=n, files=tuple(files)))
    return tuple(skills)


def role_modes(kit):
    """{role name: mode} of every role file under `<kit>/content/roles` that names a mode (cached
    per `kit`); {} when the folder is missing. A file that does not parse is left out."""
    return {name: r.mode for name, r in _roles(kit).items() if r.mode in MODES}


def roster(kit):
    """The RosterEntry of every role file under `<kit>/content/roles`, by name (cached per `kit`);
    () when the folder is missing. A file that does not parse is left out (render_content's check
    names it)."""
    return tuple(RosterEntry(r.name, r.mode, r.tier, r.group, r.run) for _, r in sorted(_roles(kit).items()))


def _roles(kit):
    """{role name: Role} of every role file under `kit` that parses (cached)."""
    key = os.path.normcase(os.path.abspath(kit))
    if key in _ROLES:
        return _ROLES[key]
    folder = _join(kit, ROLES_REL)
    out = {}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        names = []
    for n in names:
        if not n.endswith('.md'):
            continue
        try:
            with open(os.path.join(folder, n), 'rb') as f:
                role = parse_role(f.read(), '%s/%s' % (ROLES_REL, n))
        except (OSError, ContentError):
            continue
        if role.name == n[:-3]:
            out[role.name] = role
    _ROLES[key] = out
    return out


def role_mode(name, kit):
    """The mode role `name` (or the role an old name in ALIASES became) has in its role file under
    `kit`, or None."""
    if not isinstance(name, str):
        return None
    return role_modes(kit).get(ALIASES.get(name, name))


def role_writes(name, kit):
    """The `writes` paths of role `name` (an old name read as its new one) under `kit`: () when
    it names none or has no readable file."""
    if not isinstance(name, str):
        return ()
    role = _roles(kit).get(ALIASES.get(name, name))
    return role.writes if role else ()


def load(root):
    """The Content under `<root>/content`; ContentError when a file is not of its shape."""
    return Content(roles=load_roles(root), skills=load_skills(root))


def render(root, renderers, content=None, tiers=None):
    """{repo-relative posix path: bytes} of every file the renderers write: render(content), the
    manifests(kit, content) of a renderer with them, and the vendored(root) copies of kit files a
    renderer's plugin ships (read from `root`, so a copy that drifts from its source is drift); two
    renderers writing one path is a ContentError. With `tiers` ({host: {tier: model}}), also
    `<root>/docs/ROSTER.md` with its tables rendered (kitlib.roster)."""
    content = load(root) if content is None else content
    out = {}
    if tiers is not None:
        out[ROSTER_REL] = _roster_doc(root, content, tiers)
    kit = None
    for r in renderers:
        files = dict(r.render(content))
        if hasattr(r, 'manifests'):
            kit = load_kit(root) if kit is None else kit
            files.update(r.manifests(kit, content))
        if hasattr(r, 'vendored'):                  # kit files the host's plugin ships a copy of
            files.update(r.vendored(root))
        for path, data in files.items():
            if path in out:
                raise ContentError('%s: written by two hosts' % path)
            out[path] = data
    return out


def _roster_doc(root, content, tiers):
    from kitlib import roster                   # only a render reads it, never the guard's path
    try:
        with open(_join(root, ROSTER_REL), 'rb') as f:
            text = f.read().decode('utf-8')
    except (OSError, UnicodeDecodeError) as e:
        raise ContentError('%s cannot be read: %s' % (ROSTER_REL, e)) from None
    return roster.render(text, content, tiers, ROSTER_REL).encode('utf-8')


def _owned_files(root, renderers):
    out = set()
    for r in renderers:
        for rel in r.owned():
            folder = _join(root, rel)
            if os.path.isdir(folder):
                out.update('%s/%s' % (rel, f) for f in _files_under(folder))
    return out


def _read(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None


def diff(root, renderers, content=None, tiers=None):
    """(changed, stale): the rendered {path: bytes} that differ from or are missing on disk, and
    the sorted paths in an owned folder that no renderer writes."""
    want = render(root, renderers, content, tiers)
    changed = {p: d for p, d in want.items() if _read(_join(root, p)) != d}
    stale = sorted(_owned_files(root, renderers) - set(want))
    return changed, stale


def drift(root, renderers, content=None, tiers=None):
    """Every way the committed files differ from the renderers', as lines; [] when they agree."""
    changed, stale = diff(root, renderers, content, tiers)
    out = []
    for p in sorted(changed):
        out.append('%s: %s' % ('missing' if _read(_join(root, p)) is None else 'differs', p))
    out.extend('stale: %s' % p for p in stale)
    return out


def write(root, renderers, content=None, tiers=None):
    """Write every differing or missing rendered file and remove every stale one; the paths
    touched, sorted."""
    changed, stale = diff(root, renderers, content, tiers)
    for p, data in changed.items():
        path = _join(root, p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
    for p in stale:
        os.remove(_join(root, p))
    return sorted(set(changed) | set(stale))
