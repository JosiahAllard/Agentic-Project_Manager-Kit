"""The `next` block (a repo's tools-adoption block, moved into the kit): the kit commands
this session should run, with its own values filled in. Print-only, from readings already taken:
it starts no process. `--model "<your model>"` is printed literally.
"""
SHELL_UNSAFE = set('"$`\\!')
MAX_COMMIT_PATHS = 12
INDENT = ' ' * 10


def shell_word(path):
    """The path as one word in bash, PowerShell or cmd; None when double quotes cannot carry it."""
    if not path or SHELL_UNSAFE & set(path):
        return None
    plain = all(c.isalnum() or c in '._/-+@=,:' for c in path)
    return path if plain else '"%s"' % path


def committable(xy, path):
    """Whether commit.py can take this status entry as typed: not a rename or copy (two paths),
    not a whole untracked folder (`dir/`)."""
    return 'R' not in xy and 'C' not in xy and not path.endswith('/')


def lines(root, tools, branch, base, main_branch, behind, dirty, in_main):
    """The block's lines, the first labelled `next`. `dirty` is [(xy, path)] (kitlib.git.refs
    status entries); `behind` the commit count as text, or None/'?' when not read."""
    py = 'python %s/' % tools
    if in_main:
        body = [py + 'new_worktree.py <area>/<slug>']
    else:
        # Always quoted, forward slashes: Git Bash drops the backslashes of an unquoted Windows path.
        wt = '"%s"' % root.replace('\\', '/')
        entries = [(shell_word(p), committable(xy, p)) for xy, p in dirty]
        # Any entry commit.py cannot take as typed (a folder, a rename, a name no quoting carries)
        # makes the whole list a placeholder: a partial list would read as the whole change.
        words = [w for w, _ in entries]
        if entries and all(w and ok for w, ok in entries):
            shown = ' '.join(words[:MAX_COMMIT_PATHS])
            if len(words) > MAX_COMMIT_PATHS:
                shown += ' <+%d more>' % (len(words) - MAX_COMMIT_PATHS)
        else:
            shown = '<paths>'
        first = next((w for w in words if w and not w.rstrip('"').endswith('/')), None)
        body = [py + 'commit.py -C %s -m "<subject>" --model "<your model>" %s' % (wt, shown),
                py + 'review_pack.py %s' % (branch if branch and branch != 'HEAD' else '<branch>'),
                py + 'codemap.py' + (' --file %s' % first if first else '')]
        if behind not in ('0', '?', None):
            body.append(py + 'sync_base.py -C %s <hash the coordinator names> --model "<your model>"%s'
                        % (wt, '' if base == main_branch else ' --base %s' % base))
        body.append('patch scripts write through kitlib.git.checkout.write_text '
                    '(docs/worktree-paths.md), never a raw open()')
    return ['next      ' + body[0]] + [INDENT + l for l in body[1:]]
