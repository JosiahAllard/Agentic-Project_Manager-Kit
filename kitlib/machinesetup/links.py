"""Machine links, report only (docs/machine-setup.md "Links"): machine setup makes no link. The kit
runs from its own folder (the plugin root), so an account needs no `tools`, `knowledge` or `skills`
entry that points into it. This step finds the ones an earlier setup left and says how to remove
each: `cmd /c rmdir "<path>"` for a junction or directory link, `cmd /c del "<path>"` for a file
symlink (remove_cmd), each of which removes the link only, never its target (never `/s`); on
Linux `rm -- '<path>'` (REMOVE_LINUX: rm on a symlink, with no trailing slash, removes the link). It
adds no change, so a second run still has nothing to do; it runs in both modes.

What it reports, in each config folder:
- `tools`, `knowledge` or `skills` that is itself a link into this kit or the older kit clone: one
  leftover line for the folder. One that is a link anywhere else (an account folder following
  ~/.claude, or a kit clone that has moved since an earlier setup) is left alone, with one
  "skipped link" line (`skipped`), never silently.
- In a real `tools` or `knowledge` folder, each entry that is a link into this kit or the older
  clone. A real folder at one of the names an earlier setup linked (TOOL_LINKS, KNOWLEDGE_LINKS) is
  noted and left as it is; nothing is renamed or deleted.
- In a real `skills` folder, each link at a kit skill's name (a folder under SKILLS_DIR) into this
  kit (MOVED_SKILLS_DIRS included) or the older clone; the plugin loads those skills. A link at
  another name into either is noted and left: the kit has no such skill, so a repo-local skill
  moves to its repo first.
- In a real `tools`, `knowledge` or `skills` folder, an entry that links anywhere else is one
  "skipped link" line. A relative symlink target is read against the link's own folder
  (common.link_target).
- Any other entry of the config folder (`agents`, `hooks`) that is a link into the older clone is
  noted and left (docs/machine-setup.md "Not done here").

TOOL_LINKS maps each name an earlier setup linked under `<config>/tools` to the kit folder it
reached; settings.repoint_links re-points a settings path through one to the kit's own file.
"""
import os
import shlex

from kitlib import platform

from .common import MOVED_FROM, SHELL, is_link, link_target, under

TOOL_LINKS = (('machine_lock', 'tools'), ('kitlib', 'kitlib'), ('comfy', 'optional/comfy'),
              ('procwatch', 'optional/procwatch'), ('usage', 'optional/usage'),
              ('agenttemp', 'optional/agenttemp'))
KNOWLEDGE_LINKS = (('asset-generation', 'knowledge/asset-generation'),)
SKILLS_DIR = 'content/skills'                  # the skills, written once (no rendered copy)
# Where the skills were before: claude/skills before the hosts/ move, then the Claude shell's
# rendered copy until decision 29. Both are under the kit, so a link into either is a leftover.
MOVED_SKILLS_DIRS = (MOVED_FROM + '/skills', SHELL + '/skills')
CONTAINERS = ('tools', 'knowledge', 'skills')
REMOVE = 'cmd /c rmdir "%s"'                   # a junction or directory link: the link only; never /s
REMOVE_FILE = 'cmd /c del "%s"'                # a file symlink: del removes the link, not its target
REMOVE_LINUX = 'rm -- %s'                      # a symlink: the link only, never -r
DIRECTORY = 0x10                               # FILE_ATTRIBUTE_DIRECTORY


def tool_link_names():
    return [n for n, _ in TOOL_LINKS]


def kit_path(kit, rel):
    """`rel` (posix, kit-relative) under the kit."""
    return os.path.join(kit, *rel.split('/'))


def kit_skills(kit):
    """The names of the kit's skills: the folders under SKILLS_DIR."""
    d = kit_path(kit, SKILLS_DIR)
    try:
        return set(n for n in os.listdir(d) if os.path.isdir(os.path.join(d, n)))
    except OSError:
        return set()


def remove_cmd(path):
    """The command that removes the link at `path` only. Windows keeps the directory attribute on a
    junction and a directory symlink, dangling or not, and rmdir removes those; a file symlink has
    none, rmdir fails on it, and del removes the link and leaves its target. On Linux: rm."""
    if platform.NAME == 'linux':
        return REMOVE_LINUX % shlex.quote(path)
    try:
        attrs = getattr(os.lstat(path), 'st_file_attributes', DIRECTORY)
    except OSError:
        attrs = DIRECTORY
    return (REMOVE if attrs & DIRECTORY else REMOVE_FILE) % path


def leftover(path, target):
    """The note for a leftover link: where it points and the command that removes the link only."""
    return 'leftover link %s -> %s: remove it with: %s' % (path, target, remove_cmd(path))


def skipped(path, target):
    """The note for a link whose target is outside this kit (and the older clone): left as is, but
    never silent, since a link an earlier setup made into a kit clone that has moved since is one."""
    return ('skipped link %s -> %s: its target is outside this kit, left as is; if an earlier setup '
            'made it, remove it with: %s' % (path, target, remove_cmd(path)))


def _ours(target, ctx):
    """True when `target` is in this kit or the older kit clone (none in the default mode)."""
    return under(target, ctx.kit) or bool(ctx.old_kit and under(target, ctx.old_kit))


def survey(plan, d, ctx):
    skills = kit_skills(ctx.kit)
    for container in CONTAINERS:
        c = os.path.join(d, container)
        if is_link(c):
            was = link_target(c)
            if not _ours(was, ctx):
                plan.note('links', skipped(c, was))                  # e.g. follows another account folder
                continue
            plan.note('links', leftover(c, was))
            if container == 'skills':
                other = sorted(n for n in (os.listdir(was) if os.path.isdir(was) else [])
                               if os.path.isdir(os.path.join(was, n)) and n not in skills)
                if other:
                    plan.note('links', '%s also loads %s, which the kit has no skill for: a repo-local '
                                       'skill moves to its repo before the link goes' % (c, ', '.join(other)))
            continue
        if not os.path.isdir(c):
            continue
        known = {'tools': TOOL_LINKS, 'knowledge': KNOWLEDGE_LINKS}.get(container, ())
        for n in sorted(os.listdir(c)):
            p = os.path.join(c, n)
            if is_link(p):
                t = link_target(p)
                if not _ours(t, ctx):
                    plan.note('links', skipped(p, t))
                    continue
                if container == 'skills' and n not in skills:
                    plan.note('links', '%s -> %s: left as is: the kit has no skill %s (a repo-local skill '
                                       'moves to its repo)' % (p, t, n))
                else:
                    plan.note('links', leftover(p, t))
            elif os.path.isdir(p) and any(n == name for name, _ in known):
                plan.note('links', '%s: a real folder, left as it is (machine setup makes no link; the kit '
                                   'has its own copy at %s)' % (p, kit_path(ctx.kit, dict(known)[n])))
    if not ctx.old_kit:
        return
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if n not in CONTAINERS and is_link(p) and under(link_target(p), ctx.old_kit):
            plan.note('links', '%s -> %s: left as is (no kit counterpart in this step; '
                               'docs/machine-setup.md "Not done here")' % (p, link_target(p)))
