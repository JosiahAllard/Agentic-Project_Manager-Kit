"""Machine links: a config folder's `tools`, `knowledge` and `skills` entries that point into the old
kit clone become directory junctions into this kit (docs/machine-setup.md "Links").

`tools` cannot be one junction onto a kit folder: the live lock path is
`tools/machine_lock/machine_lock.py`, and the kit's tools/machine_lock.py finds kitlib beside the
folder it was started from, spelled through the junction (os.path.abspath keeps it). So `tools` is a
real folder holding one junction per entry, kitlib among them: `machine_lock` -> <kit>/tools,
`kitlib` -> <kit>/kitlib, and comfy, procwatch, usage and agenttemp -> <kit>/optional/<name>.

A link is replaced; a real folder where a link goes is renamed aside (`<name>.old-<yyyymmdd>`),
never deleted. A container (`tools`, `knowledge`, `skills`) that is a link somewhere else (another
account's folder following ~/.claude) is left alone; one that is absent is not made.

A skill link in a real `skills` folder that points into this kit's MOVED_SKILLS_DIR (the skills'
folder before the hosts/ move) is re-pointed to the same skill in SKILLS_DIR, like one into the old
kit (docs/machine-setup.md "Moved hook paths").
"""
import os

try:
    import _winapi
except ImportError:                                                  # not Windows: survey only
    _winapi = None

from .common import MOVED_FROM, SHELL, free_name, is_link, link_target, norm, under

TOOL_LINKS = (('machine_lock', 'tools'), ('kitlib', 'kitlib'), ('comfy', 'optional/comfy'),
              ('procwatch', 'optional/procwatch'), ('usage', 'optional/usage'),
              ('agenttemp', 'optional/agenttemp'))
KNOWLEDGE_LINKS = (('asset-generation', 'knowledge/asset-generation'),)
SKILLS_DIR = SHELL + '/skills'
MOVED_SKILLS_DIR = MOVED_FROM + '/skills'      # before the hosts/ move: its links are re-pointed
CONTAINERS = ('tools', 'knowledge', 'skills')


def tool_link_names():
    return [n for n, _ in TOOL_LINKS]


def make_junction(link, target):
    if _winapi is None:
        raise RuntimeError('directory junctions need Windows')
    _winapi.CreateJunction(target, link)


def kit_path(kit, rel):
    """`rel` under the kit; an absolute path (a skill kept on the old kit) as it is."""
    return rel if os.path.isabs(rel) else os.path.join(kit, *rel.split('/'))


def _skill_specs(plan, names, kit, where, old_dir=None):
    """(name, kit-relative target) per skill the kit has. One it lacks is noted; with `old_dir` (the
    container is being replaced) it is linked again to `old_dir/<name>`, so it keeps loading."""
    specs = []
    for n in names:
        if os.path.isdir(kit_path(kit, SKILLS_DIR + '/' + n)):
            specs.append((n, SKILLS_DIR + '/' + n))
            continue
        if old_dir:
            specs.append((n, os.path.join(old_dir, n)))
        plan.note('links', '%s: the kit has no skill %s; left pointing at the old kit (a repo-local '
                           'skill moves to its repo)' % (os.path.join(where, n), n))
    return specs


def survey(plan, d, ctx):
    kit, old = ctx.kit, ctx.old_kit
    for container in CONTAINERS:
        c = os.path.join(d, container)
        fresh = False
        if is_link(c):
            was = link_target(c)
            if not under(was, old):
                continue                                             # follows another folder
            if container == 'tools':
                specs = list(TOOL_LINKS)
            elif container == 'knowledge':
                specs = list(KNOWLEDGE_LINKS)
            else:
                names = sorted(n for n in os.listdir(was) if os.path.isdir(os.path.join(was, n))) \
                    if os.path.isdir(was) else []
                specs = _skill_specs(plan, names, kit, c, old_dir=was)
            kept = set(n for n, _ in specs)
            dropped = sorted(n for n in (os.listdir(was) if os.path.isdir(was) else []) if n not in kept)
            if dropped:
                plan.note('links', '%s: not carried over from %s (still there, no longer reached through '
                                   'this folder): %s' % (c, was, ', '.join(dropped)))
            plan.add('links', '%s: replace the link (-> %s) with a folder of links' % (c, was),
                     lambda c=c: (os.rmdir(c), os.mkdir(c)),
                     undo='rmdir "%s" (only if empty), then mklink /J "%s" "%s"' % (c, c, was))
            fresh = True
        elif os.path.isdir(c):
            if container == 'tools':
                specs = list(TOOL_LINKS)
            elif container == 'knowledge':
                specs = list(KNOWLEDGE_LINKS)
            else:
                moved = kit_path(kit, MOVED_SKILLS_DIR)
                names = [n for n in sorted(os.listdir(c))
                         if is_link(os.path.join(c, n))
                         and (under(link_target(os.path.join(c, n)), old)
                              or under(link_target(os.path.join(c, n)), moved))]
                specs = _skill_specs(plan, names, kit, c)
            mine = set(n for n, _ in specs)
            for n in sorted(os.listdir(c)):
                p = os.path.join(c, n)
                if n not in mine and is_link(p) and under(link_target(p), old) \
                        and not (container == 'skills' and n in names):
                    plan.note('links', '%s -> %s: left as is (no kit counterpart in this step)'
                                       % (p, link_target(p)))
        else:
            continue
        for name, rel in specs:
            _entry(plan, os.path.join(c, name), kit_path(kit, rel), rel, fresh, ctx.today)
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if n not in CONTAINERS and is_link(p) and under(link_target(p), old):
            plan.note('links', '%s -> %s: left as is (no kit counterpart in this step; '
                               'docs/machine-setup.md "Not done here")' % (p, link_target(p)))


def _entry(plan, e, want, rel, fresh, today):
    if not os.path.isdir(want):
        plan.refuse('the kit has no %s (%s would link to it)' % (rel, e))
        return
    if fresh or not os.path.lexists(e):
        plan.add('links', '%s: link -> %s' % (e, want), lambda: make_junction(e, want),
                 undo='rmdir "%s"' % e)
    elif is_link(e):
        was = link_target(e)
        if norm(was) == norm(want):
            return
        plan.add('links', '%s: re-point the link %s -> %s' % (e, was, want),
                 lambda: (os.rmdir(e), make_junction(e, want)),
                 undo='rmdir "%s", then mklink /J "%s" "%s"' % (e, e, was))
    elif os.path.isdir(e):
        aside = free_name('%s.old-%s' % (e, today))
        plan.add('links', '%s: a real folder: rename it to %s, then link -> %s'
                 % (e, os.path.basename(aside), want),
                 lambda: _aside_and_link(e, aside, want),
                 undo='rmdir "%s" (the link only), then rename "%s" back to "%s"' % (e, aside, e))
    else:
        plan.refuse('%s is a file where a link to %s goes' % (e, want))


def _aside_and_link(e, aside, want):
    os.rename(e, aside)
    try:
        make_junction(e, want)
    except BaseException:
        if not os.path.lexists(e):
            os.rename(aside, e)                                      # put the folder back
        raise
