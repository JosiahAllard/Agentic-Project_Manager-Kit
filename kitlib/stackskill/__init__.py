"""A stack's skills, copied into a repository with the kit's location written in
(tools/stack_skill.py is the entry point; docs/install.md).

A stack template (`templates/stacks/<stack>/.claude/skills/**`) names a kit tool by the placeholder
KIT_ROOT_PLACEHOLDER (`python "{kit_root}/tools/machine_lock.py" ...`): a repository holds no copy of
the kit, so the path has to be the kit's own. `plan` renders every skill file with the kit's absolute
path, forward slashes (a Git Bash command takes them, and no backslash needs escaping); the template
quotes the placeholder, so a space in the path stays one argument. `install` writes the plan. A file
already in the target is kept, never overwritten (as with the stack's tools files).

Refused, before anything is written (StackSkillRefused): a kit root that is not absolute, that lacks
tools/machine_lock.py or kitlib/, or that holds a quote, a brace or a line break (the placeholder
would be written broken); a stack the kit has not; a rendered file that still has a placeholder.
"""
import os
import re

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KIT_ROOT_PLACEHOLDER = '{kit_root}'
SKILLS_REL = os.path.join('.claude', 'skills')
TEXT_SUFFIXES = ('.md',)
_STACK = re.compile(r'^[a-z0-9]+$')


class StackSkillRefused(Exception):
    """Nothing was written."""


def resolve_kit_root(kit_root=None):
    """The kit's absolute path with forward slashes, or StackSkillRefused."""
    root = KIT_ROOT if kit_root is None else kit_root
    if not isinstance(root, str) or not os.path.isabs(root):
        raise StackSkillRefused('the kit root %r is not an absolute path' % (root,))
    if any(c in root for c in '"{}\r\n'):
        raise StackSkillRefused('the kit root %r holds a quote, a brace or a line break, so it cannot be '
                                'written into a quoted command' % (root,))
    for rel in (os.path.join('tools', 'machine_lock.py'), 'kitlib'):
        if not os.path.exists(os.path.join(root, rel)):
            raise StackSkillRefused('the kit root %s has no %s' % (root, rel))
    return os.path.normpath(root).replace('\\', '/')


def render(text, kit_root):
    """`text` with every placeholder replaced by the resolved kit root (see resolve_kit_root)."""
    return text.replace(KIT_ROOT_PLACEHOLDER, kit_root)


def plan(stack, target, kit_root=None):
    """[(destination, text or None, kept)] for the stack's skill files in `target`, sorted. `text` is
    the rendered file; `kept` is True when the destination exists already (it is left alone)."""
    kit = resolve_kit_root(kit_root)
    if not _STACK.match(stack or ''):
        raise StackSkillRefused('%r is not a stack name' % (stack,))
    src_root = os.path.join(os.path.normpath(kit), 'templates', 'stacks', stack, SKILLS_REL)
    out = []
    if not os.path.isdir(src_root):
        return out
    for here, _dirs, files in os.walk(src_root):
        for name in sorted(files):
            src = os.path.join(here, name)
            dst = os.path.join(target, SKILLS_REL, os.path.relpath(src, src_root))
            if os.path.exists(dst):
                out.append((dst, None, True))
                continue
            try:
                with open(src, 'rb') as fh:
                    raw = fh.read()
            except OSError as e:
                raise StackSkillRefused('cannot read %s: %s' % (src, e))
            if name.lower().endswith(TEXT_SUFFIXES):
                try:
                    text = raw.decode('utf-8')
                except UnicodeDecodeError:
                    raise StackSkillRefused('%s is not UTF-8' % src)
                text = render(text, kit)
                if KIT_ROOT_PLACEHOLDER in text:
                    raise StackSkillRefused('%s still has %s after rendering' % (src, KIT_ROOT_PLACEHOLDER))
                raw = text.encode('utf-8')
            out.append((dst, raw, False))
    return sorted(out)


def install(stack, target, kit_root=None, apply=False):
    """Plan, and with `apply` write; the report lines. Raises StackSkillRefused before any write."""
    steps = plan(stack, target, kit_root)
    lines = []
    for dst, raw, kept in steps:
        if kept:
            lines.append('kept     %s   (exists)' % dst)
        elif not apply:
            lines.append('would create  %s' % dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'xb') as fh:
                fh.write(raw)
            lines.append('wrote    %s' % dst)
    return lines
