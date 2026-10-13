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
the edge, tools/render_content.py). Pure file work: no printing, no exit.
"""
import json
import os
import re

from kitlib.host.content import Content, ContentError, Skill, parse_role

ROLES_REL = 'content/roles'
SKILLS_REL = 'content/skills'
KIT_REL = 'kit.json'
SKIP_DIRS = ('__pycache__',)
_KIT_NAME = re.compile(r'[a-z][a-z0-9-]*\Z')
_VERSION = re.compile(r'\d+\.\d+\.\d+\Z')


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


def load(root):
    """The Content under `<root>/content`; ContentError when a file is not of its shape."""
    return Content(roles=load_roles(root), skills=load_skills(root))


def render(root, renderers, content=None):
    """{repo-relative posix path: bytes} of every file the renderers write; two renderers writing
    one path is a ContentError."""
    content = load(root) if content is None else content
    out = {}
    kit = None
    for r in renderers:
        files = dict(r.render(content))
        if hasattr(r, 'manifests'):
            kit = load_kit(root) if kit is None else kit
            files.update(r.manifests(kit, content))
        for path, data in files.items():
            if path in out:
                raise ContentError('%s: written by two hosts' % path)
            out[path] = data
    return out


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


def diff(root, renderers, content=None):
    """(changed, stale): the rendered {path: bytes} that differ from or are missing on disk, and
    the sorted paths in an owned folder that no renderer writes."""
    want = render(root, renderers, content)
    changed = {p: d for p, d in want.items() if _read(_join(root, p)) != d}
    stale = sorted(_owned_files(root, renderers) - set(want))
    return changed, stale


def drift(root, renderers, content=None):
    """Every way the committed files differ from the renderers', as lines; [] when they agree."""
    changed, stale = diff(root, renderers, content)
    out = []
    for p in sorted(changed):
        out.append('%s: %s' % ('missing' if _read(_join(root, p)) is None else 'differs', p))
    out.extend('stale: %s' % p for p in stale)
    return out


def write(root, renderers, content=None):
    """Write every differing or missing rendered file and remove every stale one; the paths
    touched, sorted."""
    changed, stale = diff(root, renderers, content)
    for p, data in changed.items():
        path = _join(root, p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
    for p in stale:
        os.remove(_join(root, p))
    return sorted(set(changed) | set(stale))
