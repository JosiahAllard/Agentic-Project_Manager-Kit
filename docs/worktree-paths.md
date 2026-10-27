# Worktree paths

Where a write from a worktree may go. A script that opens a relative path writes wherever its cwd is, the main checkout included; the rule below joins a relative path to the worktree instead.

## The rule

`kitlib.git.repo.check_write(path, root)` judges every path:

- Allowed: a path inside the linked worktree `root`, or a path outside every git work tree (a scratch folder).
- Refused: the main checkout, another worktree, a relative path whose `..` climbs out of `root`, and a path git cannot place.
- Every path is refused when `root` is not a linked worktree root.
- A relative path joins `root`, never the cwd. Junctions and symlinks are resolved first, so one pointing into the main checkout is judged as the main checkout.

## Scratch scripts

`kitlib.git.checkout.read_text(path, root)` and `write_text(path, text, root)` apply the rule; `write_text` refuses text holding CRLF and writes LF. There is one kit copy, so a script names its worktree itself:

```python
import sys; sys.path.insert(0, r'<kit root>')
from kitlib.git import checkout
text = checkout.read_text('docs/x.md', root=r'<your worktree>')
checkout.write_text('docs/x.md', text.replace('old', 'new'), root=r'<your worktree>')
```

## CLI

`tools/worktree_paths.py [-C WORKTREE] PATH...` prints each path resolved against the work tree `WORKTREE` is in (default: the current directory's), or `REFUSED: ...`. Exit 0 when every path is allowed, 2 when one is refused or on a usage error.

Tests: tests/git/test_worktree_paths.py; `check_write` itself is tested in tests/git/test_git.py.
